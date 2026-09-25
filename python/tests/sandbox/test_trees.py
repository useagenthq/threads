"""Core's side of the Trees capability: place_tree on the in-memory fake, every export read as
the sandbox response it is, and the store's tree sink bound to its worker thread."""

import asyncio
import threading
from collections.abc import AsyncIterable, AsyncIterator, Awaitable

from sandbox_kit import OPEN
from tree_contract import BODY, EXECUTABLE, sample_tree

from threads.redaction import register
from threads.result import Err, Ok
from threads.sandbox import FakeSandbox, Trees, fake_sandbox
from threads.sandbox.protocol import ExecOutput, SandboxContext, SandboxError
from threads.sandbox.tree.tree import Tree, TreeFile
from threads.sandbox.trees import HashOnly, Misplaced, place_tree, read_tree
from threads.store import SqliteStore
from threads.store.artifacts import ArtifactSink, MemoryArtifacts

FAILED = 2


async def _fake_trees() -> Trees:
    sandbox: FakeSandbox = fake_sandbox({})
    made = await sandbox.create("op", OPEN)
    assert isinstance(made, Ok)
    session = made.value
    assert isinstance(session, Trees)
    return session


def test_place_tree_on_the_fake_keeps_modes_and_links_but_no_setuid() -> None:
    async def main() -> Tree:
        trees = await _fake_trees()
        artifacts = MemoryArtifacts()
        assert await place_tree(trees, sample_tree(artifacts), artifacts.get, OPEN) == Ok(None)
        read = await read_tree(trees, OPEN, HashOnly)
        assert isinstance(read, Ok)
        return read.value

    tree = asyncio.run(main())
    files = {e.path: e.mode for e in tree.entries if isinstance(e, TreeFile)}
    assert files == {"bin/run": EXECUTABLE, "bin/su": EXECUTABLE}
    assert [e.path for e in tree.entries if e.kind == "symlink"] == ["link"]


class _Lossy:
    """Exports the fake's tree, but its import lands nothing."""

    def __init__(self, inner: Trees) -> None:
        self._inner = inner

    async def export_tree(self, context: SandboxContext) -> Ok[ExecOutput] | Err[SandboxError]:
        return await self._inner.export_tree(context)

    async def import_tree(
        self, tar: AsyncIterable[bytes], context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        del tar, context
        return Ok(None)


def test_a_re_export_that_differs_is_tree_mismatch() -> None:
    async def main() -> object:
        artifacts = MemoryArtifacts()
        lossy = _Lossy(await _fake_trees())
        placed = await place_tree(lossy, sample_tree(artifacts), artifacts.get, OPEN)
        assert isinstance(placed, Err)
        return placed.error

    error = asyncio.run(main())
    assert isinstance(error, Misplaced)
    assert error.code == "tree_mismatch"


async def _bytes(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


class _Exporting:
    """A session whose export answers what it was given."""

    def __init__(self, code: Awaitable[int], stdout: bytes, stderr: bytes = b"") -> None:
        self._output = ExecOutput(code, _bytes(stdout), _bytes(stderr))

    async def export_tree(self, context: SandboxContext) -> Ok[ExecOutput] | Err[SandboxError]:
        del context
        return Ok(self._output)

    async def import_tree(
        self, tar: AsyncIterable[bytes], context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        raise AssertionError("not imported")


async def _done(code: int) -> int:
    return code


async def _reset() -> int:
    raise ConnectionResetError("reset")


def test_bytes_that_are_not_an_archive_are_archive_invalid_and_leave_no_task() -> None:
    async def main() -> tuple[Ok[Tree] | Err[object], int]:
        never: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        read = await read_tree(_Exporting(never, bytes([7]) * 512), OPEN, HashOnly)
        await asyncio.sleep(0)
        return read, len(asyncio.all_tasks()) - 1

    read, pending = asyncio.run(main())
    assert isinstance(read, Err)
    assert getattr(read.error, "code", None) == "archive_invalid"
    assert pending == 0, "a refused archive leaves the exit and stderr readers running"


def test_a_failed_export_is_unavailable_with_what_it_said() -> None:
    session = _Exporting(_done(FAILED), bytes(1024), b"tar: denied")
    read = asyncio.run(read_tree(session, OPEN, HashOnly))
    message = "the export of /workspace exited 2: tar: denied"
    assert read == Err(SandboxError("unavailable", message))


def test_a_transport_that_fails_after_the_archive_is_unavailable() -> None:
    read = asyncio.run(read_tree(_Exporting(_reset(), bytes(1024)), OPEN, HashOnly))
    assert isinstance(read, Err)
    assert read.error.code == "unavailable"


class _Threaded(MemoryArtifacts):
    """Records the thread every sink call runs on."""

    def __init__(self) -> None:
        super().__init__()
        self.threads: set[str] = set()

    def sink(self) -> ArtifactSink:
        self.threads.add(threading.current_thread().name)
        inner, seen = super().sink(), self.threads

        class Sink:
            def write(self, chunk: bytes) -> None:
                seen.add(threading.current_thread().name)
                inner.write(chunk)

            def commit(self) -> str:
                seen.add(threading.current_thread().name)
                return inner.commit()

            def discard(self) -> None:
                inner.discard()

        return Sink()


def test_the_store_reads_a_tree_on_its_worker_thread_unredacted() -> None:
    """Every sink call runs on the store's thread, and a registered secret in a file is stored
    as it is: redaction would change the tree's hashes (workspace inputs refuse secrets)."""
    register("/bin/sh\necho hi", "S")

    async def main(artifacts: _Threaded) -> None:
        opened = await SqliteStore.open(artifacts=artifacts)
        assert isinstance(opened, Ok)
        store = opened.value
        trees = await _fake_trees()
        source = MemoryArtifacts()
        assert await place_tree(trees, sample_tree(source), source.get, OPEN) == Ok(None)
        exported = await trees.export_tree(OPEN)
        assert isinstance(exported, Ok)
        stored = await store.put_tree(exported.value.stdout)
        assert isinstance(stored, Ok)
        assert isinstance(await store.get_artifact(stored.value.sha256), Ok)
        for entry in stored.value.tree.entries:
            if isinstance(entry, TreeFile):
                assert await store.get_artifact(entry.sha256) == Ok(BODY)
        await store.close()

    artifacts = _Threaded()
    asyncio.run(main(artifacts))
    assert artifacts.threads, "the reader opened sinks"
    assert all(name.startswith("threads-store") for name in artifacts.threads), artifacts.threads


def test_read_tree_keeps_files_through_the_store_worker() -> None:
    async def main(artifacts: _Threaded) -> None:
        opened = await SqliteStore.open(artifacts=artifacts)
        assert isinstance(opened, Ok)
        store = opened.value
        trees = await _fake_trees()
        source = MemoryArtifacts()
        assert await place_tree(trees, sample_tree(source), source.get, OPEN) == Ok(None)
        read = await read_tree(trees, OPEN, store.artifact_sink, store.offload)
        assert isinstance(read, Ok)
        files = [e for e in read.value.entries if isinstance(e, TreeFile)]
        for entry in files:
            assert await store.get_artifact(entry.sha256) == Ok(BODY)
        await store.close()

    artifacts = _Threaded()
    asyncio.run(main(artifacts))
    assert artifacts.threads
    assert all(name.startswith("threads-store") for name in artifacts.threads), artifacts.threads
