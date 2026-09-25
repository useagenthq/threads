"""The Trees capability for in-memory sandboxes (the fake, and the tests' emulated providers): a
tree held in memory, archived with the host builder and extracted with the host reader."""

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Sequence
from dataclasses import dataclass

from threads.result import Err, Ok
from threads.sandbox.protocol import ExecOutput
from threads.sandbox.tree.build import Owner, build_tar
from threads.sandbox.tree.tar import ArchiveInvalid, read_tar
from threads.sandbox.tree.tree import TreeEntry, TreeFile, TreeSymlink, sorted_tree
from threads.store.artifacts import MemoryArtifacts

WORKSPACE = "/workspace/"
_CHUNK = 64 * 1024


@dataclass(frozen=True, slots=True)
class MemoryFile:
    path: str
    """Relative to /workspace."""
    mode: int
    data: bytes


@dataclass(frozen=True, slots=True)
class MemoryLink:
    path: str
    target: str


type MemoryEntry = MemoryFile | MemoryLink


def archive_of(entries: Sequence[MemoryEntry]) -> bytes:
    """The tar archive of an in-memory tree (the builder masks modes to 0o777)."""
    artifacts = MemoryArtifacts()
    tree: list[TreeEntry] = [
        TreeSymlink(path=e.path, kind="symlink", target=e.target)
        if isinstance(e, MemoryLink)
        else TreeFile(
            path=e.path, kind="file", mode=e.mode, size=len(e.data), sha256=artifacts.put(e.data)
        )
        for e in entries
    ]
    parts: list[bytes] = []
    # Owner 0: an in-memory tree has none, and a reader ignores it.
    built = build_tar(sorted_tree(tree), artifacts.get, Owner(0, 0), parts.append)
    if isinstance(built, Err):
        raise ValueError(f"an in-memory tree is valid: {built.error.message}")
    return b"".join(parts)


async def extract(tar: AsyncIterable[bytes]) -> Ok[list[MemoryEntry]] | Err[ArchiveInvalid]:
    """An archive's files (modes as it gives them) and symlinks; directories need no entry."""
    artifacts = MemoryArtifacts()
    read = await read_tar(tar, artifacts.sink)
    if isinstance(read, Err):
        return read
    out: list[MemoryEntry] = []
    for e in read.value.entries:
        if isinstance(e, TreeSymlink):
            out.append(MemoryLink(e.path, e.target))
        elif isinstance(e, TreeFile):
            stored = artifacts.get(e.sha256)
            if isinstance(stored, Err):
                raise AssertionError(f"a file just read is stored: {e.path}")
            out.append(MemoryFile(e.path, e.mode, stored.value))
    return Ok(out)


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(data), _CHUNK):
        yield data[start : start + _CHUNK]


def exported(data: bytes) -> ExecOutput:
    """A finished export: the archive, exit code 0, nothing on stderr."""
    code: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    code.set_result(0)
    return ExecOutput(code, _chunks(data), _chunks(b""))
