"""Core's side of the Trees capability (spec/api.json `SandboxSession.export_tree`,
`import_tree`): every export is read through the strict tree reader, and a placed tree is proven
by re-exporting it. Tree bytes go straight to an artifact sink, never through the redacting
Spill: redaction would change the bytes and their hashes."""

import asyncio
import contextlib
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from threads.log import ParseError
from threads.render.artifacts import ReadArtifact
from threads.result import Err, Ok
from threads.sandbox.protocol import ExecOutput, SandboxContext, SandboxError, Trees
from threads.sandbox.tree.build import Owner, build_tar
from threads.sandbox.tree.tar import ArchiveInvalid, read_tar
from threads.sandbox.tree.tree import Tree, TreeSymlink, masked, tree_manifest_hash
from threads.store.artifacts import ArtifactSink

type ReadFailure = ArchiveInvalid | SandboxError


@dataclass(frozen=True, slots=True)
class Misplaced:
    """Why a tree couldn't be placed. Core-internal codes: workspace inputs report
    tree_mismatch as workspace_mismatch, and a host-snapshot restore as
    snapshot_manifest_mismatch."""

    code: Literal["workspace_not_empty", "tree_mismatch"]
    message: str


type PlaceFailure = ReadFailure | ParseError | Misplaced

_ROOT = Owner(0, 0)
"""--no-same-owner drops the archive's owner, so the builder's is only a placeholder."""
_STDERR_KEPT = 4096


class HashOnly:
    """A sink that only hashes: measures a tree's files without storing them."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256()

    def write(self, chunk: bytes) -> None:
        self._hash.update(chunk)

    def commit(self) -> str:
        return self._hash.hexdigest()

    def discard(self) -> None:
        return None


async def _head(stream: AsyncIterator[bytes]) -> str:
    """The head of a stream as text, draining the rest so its producer never stalls."""
    kept = bytearray()
    with contextlib.suppress(Exception):  # the exit code carries the failure
        async for chunk in stream:
            kept += chunk[: _STDERR_KEPT - len(kept)]
    return kept.decode("utf-8", "replace")


async def _exit(code: Awaitable[int]) -> Ok[int] | Err[SandboxError]:
    try:
        return Ok(await code)
    except Exception as error:
        return Err(SandboxError("unavailable", str(error)))


async def read_tree(
    session: Trees, context: SandboxContext, open_sink: Callable[[], ArtifactSink]
) -> Ok[Tree] | Err[ReadFailure]:
    """Reads the session's export of /workspace into a tree, each file into a sink from
    `open_sink` (`HashOnly` to measure, the store's `put_tree` to keep the files)."""
    exported = await session.export_tree(context)
    if isinstance(exported, Err):
        return exported
    output: ExecOutput = exported.value
    exit_code = asyncio.ensure_future(_exit(output.exit_code))
    stderr = asyncio.ensure_future(_head(output.stderr))
    tree = await read_tar(output.stdout, open_sink)
    if isinstance(tree, Err):
        # A refused archive is the answer: a hostile exporter may never exit.
        return tree
    code = await exit_code
    if isinstance(code, Err):
        return code
    if code.value != 0:
        message = f"the export of /workspace exited {code.value}: {await stderr}"
        return Err(SandboxError("unavailable", message))
    return tree


async def tree_hash(session: Trees, context: SandboxContext) -> Ok[str] | Err[ReadFailure]:
    """The manifest hash of the sandbox's /workspace, measured through its export."""
    tree = await read_tree(session, context, HashOnly)
    return tree if isinstance(tree, Err) else Ok(tree_manifest_hash(tree.value))


def _symlinks(tree: Tree) -> list[TreeSymlink]:
    return [e for e in tree.entries if isinstance(e, TreeSymlink)]


async def _each(chunks: Sequence[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def _empty(session: Trees, context: SandboxContext) -> Err[PlaceFailure] | None:
    """None when /workspace is empty; otherwise why the tree can't be placed there."""
    before = await read_tree(session, context, HashOnly)
    if isinstance(before, Err):
        return before
    if not before.value.entries:
        return None
    first = before.value.entries[0].path
    message = (
        f"the sandbox's /workspace isn't empty ({first}); workspace inputs and restores need it "
        "empty"
    )
    return Err(Misplaced("workspace_not_empty", message))


async def _holds(
    session: Trees, want: Tree, context: SandboxContext
) -> Ok[None] | Err[PlaceFailure]:
    """Whether a re-export of /workspace has `want`'s files (manifest hash) and symlinks."""
    after = await read_tree(session, context, HashOnly)
    if isinstance(after, Err):
        return after
    hashed = tree_manifest_hash(want)
    if tree_manifest_hash(after.value) == hashed and _symlinks(after.value) == _symlinks(want):
        return Ok(None)
    return Err(Misplaced("tree_mismatch", f"/workspace doesn't hold the placed tree ({hashed})"))


async def place_tree(
    session: Trees, tree: Tree, read: ReadArtifact, context: SandboxContext
) -> Ok[None] | Err[PlaceFailure]:
    """Places `tree` into the session's empty /workspace, then re-exports it and checks its
    files (manifest hash) and symlinks. Modes are compared masked: setuid, setgid and sticky
    bits never survive an import."""
    refused = await _empty(session, context)
    if refused is not None:
        return refused
    # ponytail: the archive is held whole (kits upload one file); stream it when an adapter can.
    chunks: list[bytes] = []
    built = build_tar(tree, read, _ROOT, chunks.append)
    if isinstance(built, Err):
        return built
    imported = await session.import_tree(_each(chunks), context)
    if isinstance(imported, Err):
        return imported
    return await _holds(session, masked(tree), context)
