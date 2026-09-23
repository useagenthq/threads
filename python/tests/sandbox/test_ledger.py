"""Provider resources through the ledger: the key is durable before the create,
a crash or a lost answer resolves by that key, and nothing is ever created twice."""

import asyncio
from collections.abc import Awaitable, Callable

from pydantic import JsonValue

from threads.log import BranchId, ThreadId
from threads.result import Err, Ok
from threads.sandbox import FakeSandbox, SandboxSession, fake_sandbox
from threads.sandbox.fake_session import manifest_hash
from threads.sandbox.ledger import Tracked, acquire, gc, release_session
from threads.store import SqliteStore, Writer
from threads.store.lease import TTL_MS

THREAD = ThreadId("0192a000-0000-7000-8000-000000000001")
ROOT = BranchId("0192b000-0000-7000-8000-000000000001")
T0 = 1_790_000_000_000
EMPTY = manifest_hash([])
LOST: dict[str, JsonValue] = {"snapshots": {"s": {"restore_sandbox_id": "sbx_c", "manifest": []}}}


def lost(lookup: str) -> dict[str, JsonValue]:
    snap: dict[str, JsonValue] = {
        "restore_sandbox_id": "sbx_c",
        "manifest": [],
        "restore_response": "lost",
        "create_lookup": lookup,
    }
    return {"snapshots": {"s": snap}}


def restoring(sandbox: FakeSandbox) -> Tracked[SandboxSession]:
    return Tracked(
        "sandbox",
        lambda key: sandbox.restore("s", EMPTY, key),
        sandbox.lookup,
        sandbox.info.lookup.create,
        lambda s: s.id,
    )


type Body = Callable[[SqliteStore, Writer, list[int]], Awaitable[None]]


def with_owner(body: Body) -> None:
    async def main() -> None:
        now = [T0]
        opened = await SqliteStore.open()
        assert isinstance(opened, Ok)
        store = opened.value
        try:
            assert await store.create(THREAD, ROOT, T0) == Ok(None)
            writer = await store.acquire(ROOT, "owner", lambda: now[0])
            assert isinstance(writer, Ok)
            await body(store, writer.value, now)
        finally:
            await store.close()

    asyncio.run(main())


def test_a_lost_answer_found_by_its_key_is_live_and_created_once() -> None:
    async def body(store: SqliteStore, owner: Writer, now: list[int]) -> None:
        sandbox = fake_sandbox(lost("found"))
        got = await acquire(store.ledger, owner.owner, "fake", restoring(sandbox), lambda: T0)
        assert isinstance(got, Ok)
        row, session = got.value
        assert (row.state, row.ref, session.id, sandbox.creates) == ("live", "sbx_c", "sbx_c", 1)

    with_owner(body)


def test_a_lost_answer_the_adapter_cant_resolve_parks_unknown() -> None:
    async def body(store: SqliteStore, owner: Writer, now: list[int]) -> None:
        sandbox = fake_sandbox(lost("unsupported"))
        got = await acquire(store.ledger, owner.owner, "fake", restoring(sandbox), lambda: T0)
        assert isinstance(got, Err)
        assert got.error.code == "resource_unknown"
        assert [r.state for r in await store.ledger.rows()] == ["unknown"]
        assert sandbox.creates == 1

    with_owner(body)


def test_a_typed_failure_proves_nothing_was_created() -> None:
    async def body(store: SqliteStore, owner: Writer, now: list[int]) -> None:
        sandbox = fake_sandbox()
        missing = Tracked(
            "sandbox",
            lambda key: sandbox.restore("nope", EMPTY, key),
            sandbox.lookup,
            "final",
            lambda s: s.id,
        )
        got = await acquire(store.ledger, owner.owner, "fake", missing, lambda: T0)
        assert isinstance(got, Err)
        assert got.error.code == "snapshot_missing"
        (row,) = await store.ledger.rows()
        assert (row.state, row.release_outcome) == ("released", "not_found")

    with_owner(body)


def test_a_stale_owner_never_reaches_the_provider() -> None:
    async def body(store: SqliteStore, owner: Writer, now: list[int]) -> None:
        now[0] = T0 + TTL_MS + 1
        assert isinstance(await store.acquire(ROOT, "other", lambda: now[0]), Ok)
        sandbox = fake_sandbox(LOST)
        got = await acquire(store.ledger, owner.owner, "fake", restoring(sandbox), lambda: now[0])
        assert isinstance(got, Err)
        assert got.error.code == "stale_epoch"
        assert (sandbox.creates, await store.ledger.rows()) == (0, ())

    with_owner(body)


def crashed(created: bool, lookup: str) -> tuple[str, int]:
    """A crash between the pending row and its create (or right after the create): gc resolves
    the row by its key once the owner's lease is gone."""
    result: list[tuple[str, int]] = []

    async def body(store: SqliteStore, owner: Writer, now: list[int]) -> None:
        sandbox = fake_sandbox(lost(lookup))
        row = await store.ledger.pending(owner.owner, "fake", "sandbox", T0)
        assert isinstance(row, Ok)
        if created:
            await sandbox.restore("s", EMPTY, row.value.operation_key)
        assert [r.state for r in await gc(store.ledger, sandbox, lambda: T0)] == ["pending"]
        (after,) = await gc(store.ledger, sandbox, lambda: T0 + TTL_MS)
        result.append((after.state, sandbox.creates))

    with_owner(body)
    return result[0]


def test_crash_after_pending_resolves_by_key() -> None:
    assert crashed(created=True, lookup="found") == ("live", 1)
    assert crashed(created=False, lookup="found") == ("released", 0)
    assert crashed(created=True, lookup="unsupported") == ("unknown", 1)


def test_a_failed_release_is_retried_by_gc_never_dropped() -> None:
    async def body(store: SqliteStore, owner: Writer, now: list[int]) -> None:
        sandbox = fake_sandbox(LOST)
        got = await acquire(store.ledger, owner.owner, "fake", restoring(sandbox), lambda: T0)
        assert isinstance(got, Ok)
        row, session = got.value
        sandbox.fail_releases = 2
        released = await release_session(store.ledger, owner.owner, row, session, lambda: T0)
        assert isinstance(released, Ok)
        assert released.value.state == "release_failed"
        states = [[r.state for r in await gc(store.ledger, sandbox, lambda: T0)] for _ in "ab"]
        assert states == [["release_failed"], ["released"]]
        assert isinstance(await sandbox.attach(session.id), Err)

    with_owner(body)


def test_a_stale_owner_can_not_release() -> None:
    async def body(store: SqliteStore, owner: Writer, now: list[int]) -> None:
        sandbox = fake_sandbox(LOST)
        got = await acquire(store.ledger, owner.owner, "fake", restoring(sandbox), lambda: T0)
        assert isinstance(got, Ok)
        now[0] = T0 + TTL_MS + 1
        assert isinstance(await store.acquire(ROOT, "other", lambda: now[0]), Ok)
        row, session = got.value
        refused = await release_session(store.ledger, owner.owner, row, session, lambda: now[0])
        assert isinstance(refused, Err)
        assert refused.error.code == "stale_epoch"
        assert isinstance(await sandbox.attach(session.id), Ok)

    with_owner(body)
