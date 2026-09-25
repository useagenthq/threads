"""Property tests for the tree reader: a random tree survives build then read from any
chunking; any corruption of a valid archive is a value, never an exception, and the same value
for every chunking."""

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st
from tar_kit import chunked

from threads.result import Err, Ok
from threads.sandbox.tree.build import Owner, build_tar
from threads.sandbox.tree.tar import ArchiveInvalid, read_tar
from threads.sandbox.tree.tree import (
    Tree,
    TreeDir,
    TreeEntry,
    TreeFile,
    TreeSymlink,
    encode_tree,
    masked,
    parse_tree,
    sorted_tree,
)
from threads.store.artifacts import MemoryArtifacts

NAMES = st.sampled_from(["a", "b", "é", "\U0001f600", "", "a b", "x\\y", "n" * 90])


@st.composite
def trees(draw: st.DrawFn) -> tuple[Tree, MemoryArtifacts]:
    """A random valid tree, its file bytes stored in the returned artifacts."""
    artifacts = MemoryArtifacts()
    entries: dict[str, TreeEntry] = {}
    dirs = [""]
    for _ in range(draw(st.integers(0, 12))):
        parent = draw(st.sampled_from(dirs))
        name = draw(NAMES)
        path = f"{parent}/{name}" if parent else name
        if path in entries:
            continue
        match draw(st.sampled_from(["dir", "symlink", "file", "file"])):
            case "dir":
                entries[path] = TreeDir(path=path, kind="dir", mode=draw(st.integers(0, 0o7777)))
                dirs.append(path)
            case "symlink":
                entries[path] = TreeSymlink(path=path, kind="symlink", target=draw(NAMES))
            case _:
                data = draw(st.binary(max_size=1500))
                mode = draw(st.integers(0, 0o7777))
                sha = artifacts.put(data)
                entries[path] = TreeFile(
                    path=path, kind="file", mode=mode, size=len(data), sha256=sha
                )
    return sorted_tree(tuple(entries.values())), artifacts


def _archive(tree: Tree, artifacts: MemoryArtifacts) -> bytes:
    parts: list[bytes] = []
    assert build_tar(tree, artifacts.get, Owner(1000, 1000), parts.append) == Ok(None)
    return b"".join(parts)


def _read(data: bytes, sizes: list[int]) -> Ok[Tree] | Err[ArchiveInvalid]:
    feed = iter(sizes)
    return asyncio.run(
        read_tar(chunked(data, lambda: next(feed, len(data))), MemoryArtifacts().sink)
    )


@settings(max_examples=150, deadline=None)
@given(trees(), st.lists(st.integers(0, 700)))
def test_a_tree_survives_build_masked_then_read(
    made: tuple[Tree, MemoryArtifacts], sizes: list[int]
) -> None:
    tree, artifacts = made
    assert parse_tree(encode_tree(tree)) == Ok(tree)
    assert _read(_archive(tree, artifacts), [max(1, s) for s in sizes]) == Ok(masked(tree))


@settings(max_examples=300, deadline=None)
@given(
    trees(),
    st.lists(st.tuples(st.integers(0), st.integers(0, 255)), max_size=4),
    st.one_of(st.none(), st.integers(0)),
    st.lists(st.integers(0, 64)),
)
def test_a_corrupt_archive_is_a_value_for_every_chunking(
    made: tuple[Tree, MemoryArtifacts],
    edits: list[tuple[int, int]],
    cut: int | None,
    sizes: list[int],
) -> None:
    data = bytearray(_archive(*made))
    for at, value in edits:
        data[at % len(data)] = value
    corrupt = bytes(data if cut is None else data[: cut % len(data)])
    whole = _read(corrupt, [])
    assert _read(corrupt, sizes) == whole
