"""The Trees capability of every bundled adapter (spec/api.json `SandboxSession.export_tree`,
`import_tree`), run by sandbox_contract.py over each provider's mocked backend."""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from sandbox_backend import FakeBackend
from sandbox_kit import OPEN, KitContext

from threads.result import Err, Ok
from threads.sandbox import Sandbox, SandboxSession, Trees
from threads.sandbox.tree.tree import Tree, TreeDir, TreeFile, TreeSymlink, sorted_tree
from threads.sandbox.trees import Misplaced, place_tree
from threads.store.artifacts import MemoryArtifacts

BODY = b"#!/bin/sh\necho hi\n"
EXECUTABLE = 0o755


def sample_tree(artifacts: MemoryArtifacts) -> Tree:
    """An executable, a setuid file and a symlink, the file bytes in `artifacts`."""
    sha = artifacts.put(BODY)

    def file(path: str, mode: int) -> TreeFile:
        return TreeFile(path=path, kind="file", mode=mode, size=len(BODY), sha256=sha)

    return sorted_tree(
        (
            TreeDir(path="bin", kind="dir", mode=0o755),
            file("bin/run", 0o755),
            file("bin/su", 0o4755),
            TreeSymlink(path="link", kind="symlink", target="bin/run"),
        )
    )


class _Harness(Protocol):
    @property
    def backend(self) -> FakeBackend: ...

    @property
    def sandbox(self) -> Sandbox: ...


async def _trees(h: _Harness) -> tuple[SandboxSession, Trees]:
    made = await h.sandbox.create("k1", OPEN)
    assert isinstance(made, Ok), made
    session = made.value
    assert isinstance(session, Trees), "every bundled adapter offers Trees"
    return session, session


async def place_tree_keeps_modes_and_links_and_masks_setuid(h: _Harness) -> None:
    session, trees = await _trees(h)
    artifacts = MemoryArtifacts()
    assert await place_tree(trees, sample_tree(artifacts), artifacts.get, OPEN) == Ok(None)
    box = h.backend.boxes[session.id]
    assert box.modes["/workspace/bin/run"] == EXECUTABLE
    assert box.modes["/workspace/bin/su"] == EXECUTABLE
    assert box.links["/workspace/link"] == "bin/run"
    assert not [p for p in box.files if p.endswith(".tar")], "the uploaded archive is gone"


async def place_tree_refuses_a_workspace_that_is_not_empty(h: _Harness) -> None:
    session, trees = await _trees(h)
    assert await session.upload("/workspace/left", b"x", OPEN) == Ok(None)
    artifacts = MemoryArtifacts()
    placed = await place_tree(trees, sample_tree(artifacts), artifacts.get, OPEN)
    message = (
        "the sandbox's /workspace isn't empty (left); workspace inputs and restores need it empty"
    )
    assert placed == Err(Misplaced("workspace_not_empty", message))


async def _one_block() -> AsyncIterator[bytes]:
    yield bytes(1024)


async def a_stale_context_exports_and_imports_nothing(h: _Harness) -> None:
    _, trees = await _trees(h)
    lost = KitContext(live=False)
    before = h.backend.requests
    refused = [await trees.export_tree(lost), await trees.import_tree(_one_block(), lost)]
    assert [r.error.code if isinstance(r, Err) else "ok" for r in refused] == ["stale_epoch"] * 2
    assert h.backend.requests == before, "a refused tree operation reached the provider"


TREE_CHECKS: tuple[Callable[[_Harness], Awaitable[None]], ...] = (
    place_tree_keeps_modes_and_links_and_masks_setuid,
    place_tree_refuses_a_workspace_that_is_not_empty,
    a_stale_context_exports_and_imports_nothing,
)
