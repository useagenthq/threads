"""spec/conformance/vectors: manifest-tar.json, tree.json and archive-invalid.json, each archive
read from several chunkings."""

import asyncio
import base64
from collections.abc import AsyncIterator

import pytest
from pydantic import JsonValue
from tar_kit import chunkings, vector

from threads.result import Err, Ok
from threads.sandbox.tree.build import Owner, build_tar
from threads.sandbox.tree.tar import CAPS, Caps, read_tar, store_tar
from threads.sandbox.tree.tree import encode_tree, masked, parse_tree, tree_manifest_hash
from threads.store.artifacts import MemoryArtifacts


def _cases(name: str, key: str = "cases") -> list[dict[str, JsonValue]]:
    cases = vector(name)[key]
    assert isinstance(cases, list)
    return [c for c in cases if isinstance(c, dict)]


TARS = _cases("manifest-tar.json")
TREES = {str(c["name"]): c for c in _cases("tree.json")}
BAD_TREES = _cases("tree.json", "invalid")
INVALID = _cases("archive-invalid.json")


def _bytes(case: dict[str, JsonValue]) -> bytes:
    return base64.b64decode(str(case["tar"]))


@pytest.mark.parametrize("case", TARS, ids=lambda c: str(c["name"]))
def test_manifest_tar(case: dict[str, JsonValue]) -> None:
    want = TREES[str(case["name"])]
    for i, chunks in enumerate(chunkings(_bytes(case), TARS.index(case))):
        stored = asyncio.run(store_tar(chunks(), MemoryArtifacts()))
        assert isinstance(stored, Ok), (i, stored)
        tree = stored.value.tree
        assert tree.model_dump(mode="json")["entries"] == case["entries"]
        assert encode_tree(tree).decode() == want["tree"]
        assert stored.value.sha256 == want["sha256"]
        assert stored.value.manifest_hash == want["manifest_hash"]


@pytest.mark.parametrize("case", TARS, ids=lambda c: str(c["name"]))
def test_tree_parses_back_and_rebuilds(case: dict[str, JsonValue]) -> None:
    want = TREES[str(case["name"])]
    parsed = parse_tree(str(want["tree"]).encode())
    assert isinstance(parsed, Ok)
    assert tree_manifest_hash(parsed.value) == want["manifest_hash"]
    artifacts = MemoryArtifacts()
    first = chunkings(_bytes(case), 0)[0]
    assert isinstance(asyncio.run(read_tar(first(), artifacts.sink)), Ok)
    parts: list[bytes] = []
    built = build_tar(parsed.value, artifacts.get, Owner(1000, 1000), parts.append)
    assert built == Ok(None)

    async def source() -> AsyncIterator[bytes]:
        for p in parts:
            yield p

    assert asyncio.run(read_tar(source(), MemoryArtifacts().sink)) == Ok(masked(parsed.value))


@pytest.mark.parametrize("case", BAD_TREES, ids=lambda c: str(c["name"]))
def test_refuses_a_tree(case: dict[str, JsonValue]) -> None:
    parsed = parse_tree(str(case["tree"]).encode())
    assert isinstance(parsed, Err)
    assert parsed.error.code == "artifact_corrupt"


@pytest.mark.parametrize("case", INVALID, ids=lambda c: str(c["name"]))
def test_archive_invalid(case: dict[str, JsonValue]) -> None:
    caps = case.get("caps")
    limits = CAPS
    if isinstance(caps, dict):
        file, total = caps["file"], caps["total"]
        assert isinstance(file, int)
        assert isinstance(total, int)
        limits = Caps(file, total)
    for chunks in chunkings(_bytes(case), INVALID.index(case)):
        result = asyncio.run(read_tar(chunks(), MemoryArtifacts().sink, limits))
        assert isinstance(result, Err)
        assert result.error.code == "archive_invalid"
        assert (result.error.reason, result.error.entry) == (case["reason"], case["entry"])
