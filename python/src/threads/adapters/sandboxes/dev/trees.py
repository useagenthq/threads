"""The Trees capability directly on the sandbox's host directory: no archive tool runs, and no
symlink is followed (walk.py). Modes are the directory's own; the builder masks setuid, setgid and
sticky bits out of the archive, as every other kit does.

ponytail: both directions hold one archive in memory, as `place_tree` already does; the reader's
1 GiB archive cap bounds it. Stream them when a reader wants a pull source.
"""

from collections.abc import AsyncIterable

from threads.adapters.sandboxes.dev.walk import (
    Found,
    mkdir_in,
    read_in,
    symlink_in,
    walk_tree,
    write_in,
)
from threads.log import ParseError
from threads.log.digest import sha256_hex
from threads.result import Err, Ok
from threads.sandbox.fake_trees import exported
from threads.sandbox.protocol import ExecOutput, SandboxError
from threads.sandbox.tree.build import Owner, build_tar
from threads.sandbox.tree.tar import read_tar
from threads.sandbox.tree.tree import (
    PERMISSIONS,
    Tree,
    TreeDir,
    TreeEntry,
    TreeFile,
    TreeSymlink,
    sorted_tree,
)
from threads.store.artifacts import MemoryArtifacts

_ROOT = Owner(0, 0)
"""--no-same-owner drops the archive's owner, so the builder's is only a placeholder."""


def _tree_of(
    directory: str, found: tuple[Found, ...]
) -> Ok[tuple[Tree, dict[str, tuple[str, ...]]]] | Err[SandboxError]:
    """The walked directory as a tree, each file hashed where it lies, with where each hash
    came from so the builder can read it back through the same walk."""
    entries: list[TreeEntry] = []
    at: dict[str, tuple[str, ...]] = {}
    for e in found:
        if e.kind == "dir":
            entries.append(TreeDir(path=e.path, kind="dir", mode=e.mode))
            continue
        if e.kind == "symlink":
            entries.append(TreeSymlink(path=e.path, kind="symlink", target=e.target))
            continue
        parts = tuple(e.path.split("/"))
        data = read_in(directory, parts)
        if isinstance(data, Err):
            return Err(SandboxError("unavailable", data.error.message))
        digest = sha256_hex(data.value)
        at[digest] = parts
        entries.append(
            TreeFile(path=e.path, kind="file", mode=e.mode, size=len(data.value), sha256=digest)
        )
    return Ok((sorted_tree(entries), at))


def export_dir(directory: str) -> Ok[ExecOutput] | Err[SandboxError]:
    """The directory as one tar archive, exactly as a remote sandbox's `tar -cf -` would answer."""
    found = walk_tree(directory)
    if isinstance(found, Err):
        return Err(SandboxError("unavailable", found.error.message))
    made = _tree_of(directory, found.value)
    if isinstance(made, Err):
        return made
    tree, at = made.value

    def read(digest: str) -> Ok[bytes] | Err[ParseError]:
        parts = at.get(digest)
        data = None if parts is None else read_in(directory, parts)
        if data is None or isinstance(data, Err):
            return Err(ParseError("artifact_corrupt", f"{digest} left {directory}"))
        return Ok(data.value)

    parts: list[bytes] = []
    built = build_tar(tree, read, _ROOT, parts.append)
    if isinstance(built, Err):
        return Err(SandboxError("unavailable", built.error.message))
    return Ok(exported(b"".join(parts)))


async def import_dir(directory: str, tar: AsyncIterable[bytes]) -> Ok[None] | Err[SandboxError]:
    """Extracts a host-built archive into the directory, keeping modes and symlinks."""
    artifacts = MemoryArtifacts()
    tree = await read_tar(tar, artifacts.sink)
    if isinstance(tree, Err):
        return Err(SandboxError("unavailable", tree.error.message))
    for e in tree.value.entries:
        placed = _place(directory, e, artifacts)
        if isinstance(placed, Err):
            return placed
    return Ok(None)


def _place(
    directory: str, entry: TreeEntry, artifacts: MemoryArtifacts
) -> Ok[None] | Err[SandboxError]:
    parts = tuple(entry.path.split("/"))
    if isinstance(entry, TreeDir):
        made = mkdir_in(directory, parts, entry.mode & PERMISSIONS)
    elif isinstance(entry, TreeSymlink):
        made = symlink_in(directory, parts, entry.target)
    else:
        data = artifacts.get(entry.sha256)
        if isinstance(data, Err):
            return Err(SandboxError("unavailable", data.error.message))
        made = write_in(directory, parts, data.value, entry.mode & PERMISSIONS)
    if isinstance(made, Err):
        return Err(SandboxError("unavailable", made.error.message))
    return Ok(None)
