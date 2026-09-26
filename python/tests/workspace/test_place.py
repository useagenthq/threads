"""A thread with workspace inputs (spec/schema/README.md, Materialization): the pinned tree is in
/workspace before the ledger row goes live, and a crash in that window is settled by the next
session instead of leaking a sandbox."""

import asyncio
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping

import pytest
from no_trees import NoTreesSandbox

from threads.agents.builtins import open_session
from threads.log import BranchId, ThreadId
from threads.result import Err, Ok
from threads.sandbox import FakeSandbox, fake_sandbox
from threads.sandbox.fake_session import FakeSession
from threads.sandbox.ledger import Tracked, session_lookup
from threads.sandbox.protocol import SandboxContext
from threads.sandbox.tree.tree import Tree, TreeFile
from threads.store import SqliteStore, Writer
from threads.store.resources import Resource
from threads.tools.runner import Opened

THREAD = ThreadId("0192a000-0000-7000-8000-000000000042")
ROOT = BranchId("0192b000-0000-7000-8000-000000000042")
T0 = 1_790_000_000_000
CREATES = 2
"""The crashed sandbox, then the fresh one the next session makes."""


class World:
    def __init__(self, store: SqliteStore, writer: Writer) -> None:
        self.store, self.writer = store, writer

    def clock(self) -> int:
        return T0

    async def tree(self, files: Mapping[str, str]) -> Tree:
        entries: list[TreeFile] = []
        for path, body in sorted(files.items()):
            data = body.encode()
            sha = await self.store.put_artifact(data)
            entries.append(TreeFile(kind="file", path=path, mode=0o644, size=len(data), sha256=sha))
        return Tree(tree_version=1, entries=entries)

    async def open(self, sandbox: FakeSandbox, tree: Tree | None = None) -> Opened:
        return await open_session(self.store, sandbox, self.writer, self.clock, tree)

    async def crashed(self, sandbox: FakeSandbox) -> Resource:
        """A pending row whose sandbox exists: exactly where placing a workspace runs."""
        ctx = self.store.context(self.writer.owner, self.clock)
        how = Tracked(
            "sandbox",
            lambda key: sandbox.create(key, ctx),
            *session_lookup(sandbox, ctx),
            lambda s: s.id,
        )
        pending = await self.store.ledger.pending(self.writer.owner, "fake", "sandbox", T0)
        assert isinstance(pending, Ok)
        made = await how.create(pending.value.operation_key)
        assert isinstance(made, Ok)
        return pending.value

    async def states(self) -> list[str]:
        return [r.state for r in await self.store.ledger.rows()]


def run(body: Callable[[World], Awaitable[None]]) -> None:
    async def main() -> None:
        opened = await SqliteStore.open(":memory:")
        assert isinstance(opened, Ok)
        store = opened.value
        try:
            assert await store.create(THREAD, ROOT, T0) == Ok(None)
            writer = await store.acquire(ROOT, "owner", lambda: T0)
            assert isinstance(writer, Ok)
            await body(World(store, writer.value))
        finally:
            await store.close()

    asyncio.run(main())


def test_the_pinned_tree_is_in_the_workspace_and_the_row_is_live() -> None:
    async def body(w: World) -> None:
        sandbox = fake_sandbox()
        tree = await w.tree({"NOTES.md": "# notes\n"})
        got = await w.open(sandbox, tree)
        assert isinstance(got, Ok), got
        ctx = w.store.context(w.writer.owner, w.clock)
        read = await got.value.download("/workspace/NOTES.md", ctx)
        assert isinstance(read, Ok)
        assert read.value == b"# notes\n"
        assert await w.states() == ["live"]

    run(body)


def test_a_sandbox_that_cant_take_the_tree_is_released(monkeypatch: pytest.MonkeyPatch) -> None:
    async def drop(
        _self: FakeSession, _tar: AsyncIterable[bytes], _context: SandboxContext
    ) -> Ok[None]:
        """An import that keeps nothing: the re-export can't match the pinned tree."""
        return Ok(None)

    monkeypatch.setattr(FakeSession, "import_tree", drop)

    async def body(w: World) -> None:
        sandbox = fake_sandbox()
        tree = await w.tree({"NOTES.md": "# notes\n"})
        got = await w.open(sandbox, tree)
        assert isinstance(got, Err)
        assert got.error.code == "workspace_mismatch"
        assert await w.states() == ["released"]

    run(body)


def test_a_session_without_the_trees_capability_is_capability_missing() -> None:
    async def body(w: World) -> None:
        sandbox = NoTreesSandbox(fake_sandbox())
        tree = await w.tree({"NOTES.md": "# notes\n"})
        got = await open_session(w.store, sandbox, w.writer, w.clock, tree)
        assert isinstance(got, Err)
        assert got.error.code == "capability_missing"
        assert await w.states() == ["released"]

    run(body)


def test_a_crash_between_create_and_live_is_settled_not_leaked() -> None:
    async def body(w: World) -> None:
        sandbox = fake_sandbox()
        tree = await w.tree({"NOTES.md": "# notes\n"})
        await w.crashed(sandbox)
        got = await w.open(sandbox, tree)
        assert isinstance(got, Ok), got
        assert await w.states() == ["released", "live"]
        assert sandbox.creates == CREATES

    run(body)


def test_a_crashed_capture_scratch_row_is_settled_too() -> None:
    async def body(w: World) -> None:
        # capture and fork open their sandboxes through the same pending row.
        sandbox = fake_sandbox()
        scratch = await w.crashed(sandbox)
        got = await w.open(sandbox)
        assert isinstance(got, Ok), got
        rows = await w.store.ledger.rows()
        settled = next(r for r in rows if r.resource_id == scratch.resource_id)
        assert settled.state == "released"

    run(body)


def test_without_a_workspace_nothing_is_placed() -> None:
    async def body(w: World) -> None:
        sandbox = fake_sandbox()
        got = await w.open(sandbox)
        assert isinstance(got, Ok)
        assert await w.states() == ["live"]

    run(body)
