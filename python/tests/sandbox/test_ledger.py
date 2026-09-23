"""Provider resources through the ledger: the key is durable before the create,
a crash or a lost answer resolves by that key, nothing is created twice, and every provider
call is fenced by its authority: the owner's lease or gc's claim on the row."""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import JsonValue

from threads.log import BranchId, ThreadId
from threads.result import Err, Ok
from threads.sandbox import FakeSandbox, SandboxSession, fake_sandbox
from threads.sandbox.ledger import Fenced, Tracked, abandon, acquire, gc, release_session
from threads.sandbox.manifest import manifest_hash
from threads.store import SqliteStore, Writer
from threads.store.lease import TTL_MS
from threads.store.resources import Resource

THREAD = ThreadId("0192a000-0000-7000-8000-000000000001")
ROOT = BranchId("0192b000-0000-7000-8000-000000000001")
T0 = 1_790_000_000_000
EMPTY = manifest_hash([])


def script(response: str = "ok", lookup: str = "found") -> dict[str, JsonValue]:
    snap: dict[str, JsonValue] = {"restore_sandbox_id": "sbx_c", "manifest": []}
    if response != "ok":
        snap |= {"restore_response": response, "create_lookup": lookup}
    return {"snapshots": {"s": snap}}


class World:
    def __init__(self, store: SqliteStore, writer: Writer, now: list[int]) -> None:
        self.store, self.writer, self.now = store, writer, now

    def clock(self) -> int:
        return self.now[0]

    def by(self, writer: Writer | None = None) -> Fenced:
        owner = (writer or self.writer).owner
        return Fenced(self.store.ledger, owner, self.store.context(owner, self.clock), self.clock)

    def restoring(self, sandbox: FakeSandbox, snapshot: str = "s") -> Tracked[SandboxSession]:
        context = self.by().context
        return Tracked(
            "sandbox",
            lambda key: sandbox.restore(snapshot, EMPTY, key, context),
            lambda key: sandbox.lookup(key, context),
            sandbox.info.lookup.create,
            lambda s: s.id,
        )

    async def acquire(self, sandbox: FakeSandbox) -> tuple[Resource, SandboxSession]:
        got = await acquire(
            self.store.ledger,
            self.writer.owner,
            sandbox.info.provider,
            self.restoring(sandbox),
            self.clock,
        )
        assert isinstance(got, Ok), got
        return got.value

    async def take_over(self, holder: str = "other") -> Writer:
        self.now[0] += TTL_MS + 1
        taken = await self.store.acquire(ROOT, holder, self.clock)
        assert isinstance(taken, Ok)
        return taken.value


def run(body: Callable[[World], Awaitable[None]], path: Path | str = ":memory:") -> None:
    async def main() -> None:
        now = [T0]
        opened = await SqliteStore.open(path)
        assert isinstance(opened, Ok)
        store = opened.value
        try:
            assert await store.create(THREAD, ROOT, T0) == Ok(None)
            writer = await store.acquire(ROOT, "owner", lambda: now[0])
            assert isinstance(writer, Ok)
            await body(World(store, writer.value, now))
        finally:
            await store.close()

    asyncio.run(main())


def states(rows: tuple[Resource, ...]) -> list[str]:
    return [r.state for r in rows]


def test_a_lost_answer_found_by_its_key_is_live_and_created_once() -> None:
    async def body(w: World) -> None:
        sandbox = fake_sandbox(script("lost", "found"))
        row, session = await w.acquire(sandbox)
        assert (row.state, row.ref, session.id, sandbox.creates) == ("live", "sbx_c", "sbx_c", 1)

    run(body)


def test_a_lost_answer_the_adapter_cant_resolve_parks_unknown() -> None:
    async def body(w: World) -> None:
        sandbox = fake_sandbox(script("lost", "unsupported"))
        got = await acquire(w.store.ledger, w.writer.owner, "fake", w.restoring(sandbox), w.clock)
        assert isinstance(got, Err)
        assert got.error.code == "resource_unknown"
        assert (states(await w.store.ledger.rows()), sandbox.creates) == (["unknown"], 1)

    run(body)


def test_a_typed_failure_proves_nothing_was_created() -> None:
    async def body(w: World) -> None:
        sandbox = fake_sandbox()
        how = w.restoring(sandbox, "nope")
        got = await acquire(w.store.ledger, w.writer.owner, "fake", how, w.clock)
        assert isinstance(got, Err)
        assert got.error.code == "snapshot_missing"
        (row,) = await w.store.ledger.rows()
        assert (row.state, row.release_outcome) == ("released", "not_found")

    run(body)


def _after[T](
    first: Callable[[], Awaitable[object]], then: Callable[[str], Awaitable[T]]
) -> Callable[[str], Awaitable[T]]:
    async def call(key: str) -> T:
        await first()
        return await then(key)

    return call


def test_a_takeover_during_the_restore_creates_nothing() -> None:
    """the owner loses its lease between writing the pending row and the provider
    call; the adapter's fence at dispatch refuses, so nothing is created behind the new owner's
    back."""

    async def body(w: World) -> None:
        sandbox = fake_sandbox(script())
        how = w.restoring(sandbox)
        stolen = Tracked("sandbox", _after(w.take_over, how.create), how.lookup, "final", how.ref)
        got = await acquire(w.store.ledger, w.writer.owner, "fake", stolen, w.clock)
        assert isinstance(got, Err)
        assert got.error.code == "stale_epoch"
        assert (sandbox.creates, states(await w.store.ledger.rows())) == (0, ["pending"])

    run(body)


def test_a_stale_owner_never_reaches_the_provider() -> None:
    async def body(w: World) -> None:
        await w.take_over()
        sandbox = fake_sandbox(script())
        got = await acquire(w.store.ledger, w.writer.owner, "fake", w.restoring(sandbox), w.clock)
        assert isinstance(got, Err)
        assert got.error.code == "stale_epoch"
        assert (sandbox.creates, await w.store.ledger.rows()) == (0, ())

    run(body)


def crashed(created: bool, lookup: str) -> tuple[str, str | None, int]:
    """A crash between the pending row and its create (or right after the create): the next
    owner of the branch resolves the row by its key and gives it up."""
    result: list[tuple[str, str | None, int]] = []

    async def body(w: World) -> None:
        sandbox = fake_sandbox(script("lost", lookup))
        row = await w.store.ledger.pending(w.writer.owner, "fake", "sandbox", T0)
        assert isinstance(row, Ok)
        if created:
            await sandbox.restore("s", EMPTY, row.value.operation_key, w.by().context)
        after = await abandon(w.by(await w.take_over()), sandbox, row.value)
        result.append((after.state, after.release_outcome, sandbox.creates))

    run(body)
    return result[0]


def test_crash_after_pending_resolves_by_key() -> None:
    assert crashed(created=True, lookup="found") == ("released", "released", 1)
    assert crashed(created=False, lookup="found") == ("released", "not_found", 0)
    assert crashed(created=True, lookup="unsupported") == ("unknown", None, 1)


async def failed_release(w: World, sandbox: FakeSandbox) -> SandboxSession:
    row, session = await w.acquire(sandbox)
    sandbox.fail_releases = 1
    released = await release_session(w.by(), row, session)
    assert isinstance(released, Ok)
    assert released.value.state == "release_failed"
    return session


def test_a_failed_release_is_retried_by_gc_never_dropped() -> None:
    async def body(w: World) -> None:
        sandbox = fake_sandbox(script())
        session = await failed_release(w, sandbox)
        sandbox.fail_releases = 1
        assert states(await gc(w.store, sandbox, w.clock)) == ["release_failed"]
        assert states(await gc(w.store, sandbox, w.clock)) == ["released"]
        assert isinstance(await sandbox.attach(session.id, w.by().context), Err)

    run(body)


def test_gc_releases_after_the_owning_branch_is_deleted(tmp_path: Path) -> None:
    path = tmp_path / "threads.db"

    async def body(w: World) -> None:
        sandbox = fake_sandbox(script())
        await failed_release(w, sandbox)
        # `threads delete` removes the branch and its lease; the ledger row outlives them.
        with sqlite3.connect(path) as conn:
            for table in ("leases", "events", "branches"):
                conn.execute(f"DELETE FROM {table} WHERE branch_id = ?", (ROOT,))  # noqa: S608 - fixed tables
        assert states(await gc(w.store, sandbox, w.clock)) == ["released"]

    run(body, path)


def test_two_concurrent_gc_runs_dispatch_one_release() -> None:
    async def body(w: World) -> None:
        sandbox = fake_sandbox(script())
        await failed_release(w, sandbox)
        before = sandbox.releases
        await asyncio.gather(gc(w.store, sandbox, w.clock), gc(w.store, sandbox, w.clock))
        assert sandbox.releases - before == 1
        assert states(await w.store.ledger.rows()) == ["released"]

    run(body)


def test_only_the_rows_own_provider_touches_it() -> None:
    async def body(w: World) -> None:
        other = FakeSandbox(fake_sandbox(script()).script, provider="other")
        sandbox = fake_sandbox(script())
        await failed_release(w, other)
        pending = await w.store.ledger.pending(w.writer.owner, "other", "sandbox", w.clock())
        assert isinstance(pending, Ok)
        assert states(await gc(w.store, sandbox, w.clock)) == ["release_failed", "pending"]
        assert await abandon(w.by(), sandbox, pending.value) == pending.value
        assert (sandbox.creates, sandbox.releases) == (0, 0)
        assert states(await gc(w.store, other, w.clock)) == ["released", "pending"]

    run(body)


def test_a_stale_owner_can_not_release() -> None:
    async def body(w: World) -> None:
        sandbox = fake_sandbox(script())
        row, session = await w.acquire(sandbox)
        stale = w.by()
        await w.take_over()
        refused = await release_session(stale, row, session)
        assert isinstance(refused, Err)
        assert refused.error.code == "stale_epoch"
        assert sandbox.releases == 0

    run(body)
