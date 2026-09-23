"""The ledger's crash and takeover rules against a real adapter over its mocked
backend: the key is durable before the create, a takeover mid-create creates nothing, a crash
resolves by the key with the adapter's declared finality, and a failed release is retried by
gc, one release per row however many gc runs race."""

import asyncio
from collections.abc import Awaitable, Callable

from sandbox_backend import FakeBackend
from sandbox_contract import Make

from threads.log import BranchId, ThreadId
from threads.result import Err, Ok
from threads.sandbox import Sandbox, SandboxError, SandboxSession
from threads.sandbox.ledger import Fenced, Tracked, abandon, acquire, gc, release_session
from threads.store import SqliteStore, Writer
from threads.store.lease import TTL_MS
from threads.store.resources import Resource

THREAD = ThreadId("0192a000-0000-7000-8000-000000000001")
ROOT = BranchId("0192b000-0000-7000-8000-000000000001")
T0 = 1_790_000_000_000


class World:
    def __init__(
        self, store: SqliteStore, writer: Writer, backend: FakeBackend, sandbox: Sandbox
    ) -> None:
        self.store, self.writer, self.backend, self.sandbox = store, writer, backend, sandbox
        self.now = T0

    def clock(self) -> int:
        return self.now

    def by(self, writer: Writer | None = None) -> Fenced:
        owner = (writer or self.writer).owner
        return Fenced(self.store.ledger, owner, self.store.context(owner, self.clock), self.clock)

    def creating(
        self, first: Callable[[], Awaitable[object]] | None = None
    ) -> Tracked[SandboxSession]:
        context = self.by().context

        async def create(key: str) -> Ok[SandboxSession] | Err[SandboxError]:
            if first is not None:
                await first()
            return await self.sandbox.create(key, context)

        return Tracked(
            "sandbox",
            create,
            lambda key: self.sandbox.lookup(key, context),
            self.sandbox.info.lookup.create,
            lambda s: s.id,
        )

    async def acquire(self) -> tuple[Resource, SandboxSession]:
        provider = self.sandbox.info.provider
        got = await acquire(
            self.store.ledger, self.writer.owner, provider, self.creating(), self.clock
        )
        assert isinstance(got, Ok), got
        return got.value

    async def take_over(self) -> Writer:
        self.now += TTL_MS + 1
        taken = await self.store.acquire(ROOT, "other", self.clock)
        assert isinstance(taken, Ok)
        return taken.value


type Body = Callable[[World], Awaitable[None]]


async def run_ledger(body: Body, make: Make) -> None:
    backend = FakeBackend.scripted()
    async with make(backend, "under-test") as sandbox:
        opened = await SqliteStore.open()
        assert isinstance(opened, Ok)
        store = opened.value
        try:
            assert await store.create(THREAD, ROOT, T0) == Ok(None)
            writer = await store.acquire(ROOT, "owner", lambda: T0)
            assert isinstance(writer, Ok)
            await body(World(store, writer.value, backend, sandbox))
        finally:
            await store.close()


def states(rows: tuple[Resource, ...]) -> list[str]:
    return [r.state for r in rows]


async def a_takeover_mid_create_creates_nothing(w: World) -> None:
    """the lease moves between the pending row and the provider call; the fence at
    the transport refuses, so nothing is created behind the new owner's back."""
    provider = w.sandbox.info.provider
    got = await acquire(w.store.ledger, w.writer.owner, provider, w.creating(w.take_over), w.clock)
    assert isinstance(got, Err)
    assert got.error.code == "stale_epoch"
    assert (w.backend.creates, states(await w.store.ledger.rows())) == (0, ["pending"])


async def a_lost_answer_found_by_its_key_is_live_once(w: World) -> None:
    w.backend.lose_creates = 1
    row, session = await w.acquire()
    assert (row.state, row.ref, w.backend.creates) == ("live", session.id, 1)


async def a_crash_after_pending_resolves_by_key(w: World) -> None:
    """The next owner resolves a crashed owner's pending rows by their keys: a created sandbox
    is found and released; an uncreated one is released only on a final lookup."""
    provider = w.sandbox.info.provider
    created = await w.store.ledger.pending(w.writer.owner, provider, "sandbox", T0)
    never = await w.store.ledger.pending(w.writer.owner, provider, "sandbox", T0)
    assert isinstance(created, Ok)
    assert isinstance(never, Ok)
    made = await w.sandbox.create(created.value.operation_key, w.by().context)
    assert isinstance(made, Ok)
    new = w.by(await w.take_over())
    after = await abandon(new, w.sandbox, created.value)
    assert (after.state, after.release_outcome) == ("released", "released")
    assert w.backend.get(made.value.id) is None
    unproven = await abandon(new, w.sandbox, never.value)
    final = w.sandbox.info.lookup.create == "final"
    assert unproven.state == ("released" if final else "unknown")


async def a_failed_release_is_retried_by_gc(w: World) -> None:
    row, session = await w.acquire()
    w.backend.fail_releases = 1
    released = await release_session(w.by(), row, session)
    assert isinstance(released, Ok)
    assert released.value.state == "release_failed"
    before = w.backend.releases
    await asyncio.gather(gc(w.store, w.sandbox, w.clock), gc(w.store, w.sandbox, w.clock))
    assert w.backend.releases - before == 1
    assert states(await w.store.ledger.rows()) == ["released"]
    assert w.backend.get(session.id) is None


async def a_stale_owner_can_not_release(w: World) -> None:
    row, session = await w.acquire()
    stale = w.by()
    await w.take_over()
    refused = await release_session(stale, row, session)
    assert isinstance(refused, Err)
    assert refused.error.code == "stale_epoch"
    assert w.backend.releases == 0


LEDGER: tuple[Body, ...] = (
    a_takeover_mid_create_creates_nothing,
    a_lost_answer_found_by_its_key_is_live_once,
    a_crash_after_pending_resolves_by_key,
    a_failed_release_is_retried_by_gc,
    a_stale_owner_can_not_release,
)
