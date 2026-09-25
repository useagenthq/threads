"""The review's attacks: a deep pax path is refused in linear time, a long zero trailer is
scanned in bulk, and the builder refuses a tree that breaks the tree rules, wherever the tree
came from."""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from threads.result import Err, Ok
from threads.sandbox.tree.build import Owner, build_tar
from threads.sandbox.tree.tar import read_tar
from threads.sandbox.tree.tree import Tree, TreeDir, TreeSymlink, parse_tree
from threads.store.artifacts import MemoryArtifacts

QUICK_S = 0.25
"""Well under a second: the refusal is linear, not quadratic."""


def _header(name: bytes, kind: bytes, size: int) -> bytes:
    """A ustar header by hand, since the builder refuses the paths under test."""
    block = bytearray(512)
    block[0 : len(name)] = name
    block[100:108] = b"0000644\0"
    block[108:124] = b"0000000\0" * 2
    block[124:136] = b"%011o\0" % size
    block[136:148] = b"00000000000\0"
    block[156:157] = kind
    block[257:265] = b"ustar\x0000"
    block[148:156] = b" " * 8
    block[148:156] = b"%06o\0 " % sum(block)
    return bytes(block)


def _pax_archive(path: str) -> bytes:
    value = path.encode()
    body = 3 + len(b"path") + len(value)
    length = body + len(str(body))
    if len(str(length)) > len(str(body)):
        length += 1
    record = b"%d path=%s\n" % (length, value)
    padded = record + bytes(-len(record) % 512)
    return (
        _header(b"././@PaxHeader", b"x", len(record))
        + padded
        + _header(b"f", b"0", 0)
        + bytes(1024)
    )


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


def test_a_40k_component_pax_path_is_refused_in_well_under_a_second() -> None:
    data = _pax_archive("a/" * 40_000 + "x")
    started = time.perf_counter()
    read = asyncio.run(read_tar(_once(data), MemoryArtifacts().sink))
    assert time.perf_counter() - started < QUICK_S
    assert isinstance(read, Err)
    assert read.error.reason == "bad_path"


def test_a_40k_component_tree_path_is_refused_in_well_under_a_second() -> None:
    tree = (
        '{"entries":[{"kind":"dir","mode":493,"path":"' + "a/" * 40_000 + 'x"}],"tree_version":1}'
    )
    started = time.perf_counter()
    parsed = parse_tree(tree.encode())
    assert time.perf_counter() - started < QUICK_S
    assert isinstance(parsed, Err)
    assert parsed.error.code == "artifact_corrupt"


def test_a_long_zero_trailer_is_scanned_in_bulk() -> None:
    data = _header(b"f", b"0", 0) + bytes(64 * 2**20)
    started = time.perf_counter()
    read = asyncio.run(read_tar(_once(data), MemoryArtifacts().sink))
    assert time.perf_counter() - started < 1
    assert isinstance(read, Ok)


@pytest.mark.parametrize(
    "tree",
    [
        Tree(tree_version=1, entries=[TreeDir(path="../evil", kind="dir", mode=0o755)]),
        Tree(tree_version=1, entries=[TreeSymlink(path="l", kind="symlink", target="/etc")]),
        Tree(
            tree_version=1,
            entries=[
                TreeSymlink(path="l", kind="symlink", target="d"),
                TreeDir(path="l/x", kind="dir", mode=0o755),
            ],
        ),
    ],
)
def test_the_builder_refuses_a_tree_that_breaks_the_tree_rules(tree: Tree) -> None:
    chunks: list[bytes] = []
    built = build_tar(tree, MemoryArtifacts().get, Owner(1000, 1000), chunks.append)
    assert isinstance(built, Err)
    assert built.error.code == "artifact_corrupt"
    assert chunks == []
